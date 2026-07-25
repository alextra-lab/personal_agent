/**
 * ADR-0123 T3 (FRE-936) — the live phase surface's rendering component.
 *
 * Covers: renders nothing when idle; the 4 terminal-state treatments;
 * concurrent children indented under their parent; an orphan child (parent
 * dropped by best-effort emission) still renders; escalating candour past
 * the threshold; ticking elapsed (AC-5 integration); and never a
 * progress bar / percentage (ADR §4 — explicitly forbidden).
 */

import { render, screen } from '@testing-library/react';
import { act } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { PhaseIndicator } from '@/components/PhaseIndicator';
import type { PhaseNode } from '@/lib/types';

function node(overrides: Partial<PhaseNode>): PhaseNode {
  return {
    phaseId: 'p1',
    phase: 'planning',
    detail: null,
    startedAt: '2026-07-25T10:00:00.000Z',
    state: 'running',
    parentId: null,
    endedAt: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-07-25T10:00:05.000Z')); // +5s
});

afterEach(() => {
  vi.useRealTimers();
});

describe('PhaseIndicator', () => {
  it('renders nothing for an empty phase list', () => {
    const { container } = render(<PhaseIndicator phases={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders a running phase with a live elapsed counter', () => {
    render(<PhaseIndicator phases={[node({ state: 'running' })]} />);
    const row = screen.getByTestId('phase-p1');
    expect(row).toHaveAttribute('data-state', 'running');
    expect(row.textContent).toMatch(/5s/);
  });

  it('renders a completed phase with a checkmark treatment', () => {
    render(
      <PhaseIndicator
        phases={[node({ state: 'completed', endedAt: new Date('2026-07-25T10:00:03.000Z').getTime() })]}
      />,
    );
    const row = screen.getByTestId('phase-p1');
    expect(row).toHaveAttribute('data-state', 'completed');
    expect(row.textContent).toMatch(/3s/);
  });

  it('renders a cancelled phase distinctly, not as a running spinner', () => {
    render(
      <PhaseIndicator
        phases={[node({ state: 'cancelled', endedAt: new Date('2026-07-25T10:00:02.000Z').getTime() })]}
      />,
    );
    const row = screen.getByTestId('phase-p1');
    expect(row).toHaveAttribute('data-state', 'cancelled');
    expect(row.textContent?.toLowerCase()).toContain('cancelled');
  });

  it('renders an error phase distinctly, not as a completed checkmark', () => {
    render(
      <PhaseIndicator
        phases={[node({ state: 'error', endedAt: new Date('2026-07-25T10:00:02.000Z').getTime() })]}
      />,
    );
    const row = screen.getByTestId('phase-p1');
    expect(row).toHaveAttribute('data-state', 'error');
    expect(row.textContent?.toLowerCase()).toContain('failed');
  });

  it('never renders a progress bar or a percentage (ADR §4 — explicitly forbidden)', () => {
    render(
      <PhaseIndicator
        phases={[node({ state: 'running', startedAt: '2026-07-25T09:58:00.000Z' })]}
      />,
    );
    expect(screen.queryByRole('progressbar')).toBeNull();
    expect(document.body.textContent).not.toMatch(/%/);
  });

  it('groups concurrent children under their parent, each independently stated', () => {
    const phases: PhaseNode[] = [
      node({ phaseId: 'e1', phase: 'expansion', state: 'running', detail: '2 sub-agents' }),
      node({ phaseId: 'c1', phase: 'sub_agent', parentId: 'e1', state: 'completed', detail: 'pricing history', endedAt: Date.now() }),
      node({ phaseId: 'c2', phase: 'sub_agent', parentId: 'e1', state: 'running', detail: 'competitor set' }),
    ];
    render(<PhaseIndicator phases={phases} />);

    const parent = screen.getByTestId('phase-e1');
    const child1 = screen.getByTestId('phase-c1');
    const child2 = screen.getByTestId('phase-c2');

    expect(parent).toHaveAttribute('data-state', 'running');
    expect(child1).toHaveAttribute('data-state', 'completed');
    expect(child2).toHaveAttribute('data-state', 'running');
    // Children render nested inside their parent's subtree.
    expect(parent.contains(child1)).toBe(true);
    expect(parent.contains(child2)).toBe(true);
  });

  it('renders an orphan child (parent never arrived — dropped by best-effort emission) as top-level rather than silently disappearing', () => {
    render(
      <PhaseIndicator
        phases={[node({ phaseId: 'c1', phase: 'sub_agent', parentId: 'missing-parent', state: 'running' })]}
      />,
    );
    expect(screen.getByTestId('phase-c1')).toBeInTheDocument();
  });

  it('adds escalating-candour static text once a running phase exceeds the threshold', () => {
    const { rerender } = render(
      <PhaseIndicator phases={[node({ state: 'running', startedAt: '2026-07-25T10:00:00.000Z' })]} />,
    );
    // At +5s (below the 60s threshold): no candour text yet.
    expect(screen.getByTestId('phase-p1').textContent).not.toMatch(/take/i);

    act(() => {
      vi.setSystemTime(new Date('2026-07-25T10:02:10.000Z')); // +130s
    });
    rerender(<PhaseIndicator phases={[node({ state: 'running', startedAt: '2026-07-25T10:00:00.000Z' })]} />);
    // The component's own ticking interval also fires; advance it explicitly.
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getByTestId('phase-p1').textContent).toMatch(/take/i);
  });

  it('AC-5: the displayed elapsed value advances at two sampled instants ≥30s apart, without a remount', () => {
    render(<PhaseIndicator phases={[node({ state: 'running', startedAt: '2026-07-25T10:00:00.000Z' })]} />);

    const readSeconds = (): number => {
      const match = screen.getByTestId('phase-p1').textContent?.match(/(\d+)s/);
      return match ? Number(match[1]) : NaN;
    };

    const first = readSeconds(); // ~5s
    act(() => {
      vi.setSystemTime(new Date('2026-07-25T10:00:40.000Z')); // +40s from start
      vi.advanceTimersByTime(1000); // let the ticking interval fire
    });
    const second = readSeconds();

    expect(second).toBeGreaterThan(first);
    expect(second - first).toBeGreaterThanOrEqual(30);
  });
});
