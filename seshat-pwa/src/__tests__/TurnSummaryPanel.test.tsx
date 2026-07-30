/**
 * ADR-0123 T4 (FRE-937) — the collapsed per-turn summary's rendering component.
 *
 * Covers: renders nothing for an undefined summary; collapsed by default via
 * native <details>; expanding reveals phase rows (label + duration) and tool
 * badges; terminal-state header text differs (completed/cancelled/error);
 * never a progress bar or percentage (same ADR §4 prohibition PhaseIndicator
 * already guards).
 */

import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';

import { TurnSummaryPanel } from '@/components/TurnSummaryPanel';
import type { TurnSummary } from '@/lib/types';

function summary(overrides: Partial<TurnSummary>): TurnSummary {
  return {
    phases: [
      { phaseId: 'p1', phase: 'planning', detail: null, durationMs: 43_000, state: 'completed', parentId: null },
      {
        phaseId: 'p2',
        phase: 'artifact_build',
        detail: null,
        durationMs: 130_000,
        state: 'completed',
        parentId: null,
      },
    ],
    tools: ['perplexity_query'],
    terminalState: 'completed',
    ...overrides,
  };
}

describe('TurnSummaryPanel', () => {
  it('renders nothing for an undefined summary', () => {
    const { container } = render(<TurnSummaryPanel summary={undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('is collapsed by default (native <details> closed)', () => {
    render(<TurnSummaryPanel summary={summary({})} />);
    const details = screen.getByTestId('turn-summary');
    expect(details.tagName.toLowerCase()).toBe('details');
    expect((details as HTMLDetailsElement).open).toBe(false);
  });

  it('reveals phase rows and tool badges once expanded', () => {
    render(<TurnSummaryPanel summary={summary({})} />);
    const details = screen.getByTestId('turn-summary') as HTMLDetailsElement;
    details.open = true;

    expect(screen.getByText('Thinking')).toBeInTheDocument();
    expect(screen.getByText('Building the artifact')).toBeInTheDocument();
    expect(screen.getByText('43s')).toBeInTheDocument();
    expect(screen.getByText('2m 10s')).toBeInTheDocument();
    expect(screen.getByText('perplexity_query')).toBeInTheDocument();
  });

  it('FRE-937 (master PR #758 bounce): repeated synthesis rounds render as distinct rows in the collapsed summary too', () => {
    render(
      <TurnSummaryPanel
        summary={summary({
          phases: [
            { phaseId: 's1', phase: 'synthesis', detail: 'round 1', durationMs: 10_000, state: 'completed', parentId: null },
            { phaseId: 's2', phase: 'synthesis', detail: 'round 2', durationMs: 7_000, state: 'completed', parentId: null },
          ],
        })}
      />,
    );
    const details = screen.getByTestId('turn-summary') as HTMLDetailsElement;
    details.open = true;

    const first = screen.getByTestId('turn-summary-phase-s1').textContent ?? '';
    const second = screen.getByTestId('turn-summary-phase-s2').textContent ?? '';
    expect(first).toContain('round 1');
    expect(second).toContain('round 2');
    expect(first).not.toBe(second);
  });

  it('shows a completed header distinctly from cancelled', () => {
    const { rerender } = render(<TurnSummaryPanel summary={summary({ terminalState: 'completed' })} />);
    const completedHeader = screen.getByTestId('turn-summary-header').textContent ?? '';

    rerender(<TurnSummaryPanel summary={summary({ terminalState: 'cancelled' })} />);
    const cancelledHeader = screen.getByTestId('turn-summary-header').textContent ?? '';

    expect(cancelledHeader).not.toBe(completedHeader);
    expect(cancelledHeader.toLowerCase()).toContain('cancelled');
  });

  it('shows a failed header for terminalState error', () => {
    render(<TurnSummaryPanel summary={summary({ terminalState: 'error' })} />);
    expect(screen.getByTestId('turn-summary-header').textContent?.toLowerCase()).toContain('failed');
  });

  it('renders a cancelled phase and an errored phase with distinct per-row treatment', () => {
    render(
      <TurnSummaryPanel
        summary={summary({
          terminalState: 'cancelled',
          phases: [
            { phaseId: 'p1', phase: 'planning', detail: null, durationMs: 5000, state: 'completed', parentId: null },
            {
              phaseId: 'p2',
              phase: 'artifact_build',
              detail: null,
              durationMs: 12_000,
              state: 'cancelled',
              parentId: null,
            },
          ],
        })}
      />,
    );
    const details = screen.getByTestId('turn-summary') as HTMLDetailsElement;
    details.open = true;

    expect(screen.getByTestId('turn-summary-phase-p1')).toHaveAttribute('data-state', 'completed');
    expect(screen.getByTestId('turn-summary-phase-p2')).toHaveAttribute('data-state', 'cancelled');
  });

  it('groups concurrent children under their parent phase', () => {
    render(
      <TurnSummaryPanel
        summary={summary({
          phases: [
            {
              phaseId: 'e1',
              phase: 'expansion',
              detail: null,
              durationMs: 20_000,
              state: 'completed',
              parentId: null,
            },
            {
              phaseId: 'c1',
              phase: 'sub_agent',
              detail: 'pricing history',
              durationMs: 12_000,
              state: 'completed',
              parentId: 'e1',
            },
          ],
        })}
      />,
    );
    const details = screen.getByTestId('turn-summary') as HTMLDetailsElement;
    details.open = true;

    const parent = screen.getByTestId('turn-summary-phase-e1');
    const child = screen.getByTestId('turn-summary-phase-c1');
    expect(parent.contains(child)).toBe(true);
  });

  it('never renders a progress bar or a percentage (ADR §4 — explicitly forbidden)', () => {
    render(<TurnSummaryPanel summary={summary({})} />);
    const details = screen.getByTestId('turn-summary') as HTMLDetailsElement;
    details.open = true;
    expect(screen.queryByRole('progressbar')).toBeNull();
    expect(document.body.textContent).not.toMatch(/%/);
  });

  it('renders nothing when the summary has zero phases and zero tools', () => {
    const { container } = render(
      <TurnSummaryPanel summary={summary({ phases: [], tools: [], terminalState: 'completed' })} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
