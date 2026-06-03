/**
 * Tests for TurnStatusBar component (ADR-0076 / FRE-400 WS2).
 */

import { render, screen } from '@testing-library/react';
import { vi, describe, it, expect } from 'vitest';

vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
}));

import { TurnStatusBar } from '@/components/TurnStatusBar';
import type { TurnStatus } from '@/lib/types';

function makeStatus(overrides: Partial<TurnStatus> = {}): TurnStatus {
  return {
    context_tokens: 10000,
    context_max: 100000,
    tool_iteration: 1,
    tool_iteration_max: 10,
    turn_cost_usd: 0.01,
    trace_id: 'test-trace',
    ...overrides,
  };
}

describe('TurnStatusBar — null renders nothing', () => {
  it('renders nothing when status is null', () => {
    const { container } = render(<TurnStatusBar status={null} />);
    expect(container.firstChild).toBeNull();
  });
});

describe('TurnStatusBar — context bar colour thresholds', () => {
  it('shows green bar below 70%', () => {
    render(<TurnStatusBar status={makeStatus({ context_tokens: 60000, context_max: 100000 })} />);
    const bar = document.querySelector('.bg-emerald-500');
    expect(bar).not.toBeNull();
  });

  it('shows amber bar at exactly 70%', () => {
    render(<TurnStatusBar status={makeStatus({ context_tokens: 70000, context_max: 100000 })} />);
    const bar = document.querySelector('.bg-amber-500');
    expect(bar).not.toBeNull();
  });

  it('shows amber bar between 70% and 84%', () => {
    render(<TurnStatusBar status={makeStatus({ context_tokens: 80000, context_max: 100000 })} />);
    const bar = document.querySelector('.bg-amber-500');
    expect(bar).not.toBeNull();
  });

  it('shows red bar at exactly 85%', () => {
    render(<TurnStatusBar status={makeStatus({ context_tokens: 85000, context_max: 100000 })} />);
    const bar = document.querySelector('.bg-red-500');
    expect(bar).not.toBeNull();
  });

  it('shows red bar above 85%', () => {
    render(<TurnStatusBar status={makeStatus({ context_tokens: 95000, context_max: 100000 })} />);
    const bar = document.querySelector('.bg-red-500');
    expect(bar).not.toBeNull();
  });
});

describe('TurnStatusBar — tool iteration colour', () => {
  it('shows amber tools label when iteration is at max-2', () => {
    render(<TurnStatusBar status={makeStatus({ tool_iteration: 8, tool_iteration_max: 10 })} />);
    const toolSpan = screen.getByText(/tools 8\/10/);
    expect(toolSpan.className).toContain('text-amber-400');
  });

  it('shows normal (slate) tools label when iteration is below max-2', () => {
    render(<TurnStatusBar status={makeStatus({ tool_iteration: 5, tool_iteration_max: 10 })} />);
    const toolSpan = screen.getByText(/tools 5\/10/);
    expect(toolSpan.className).toContain('text-slate-400');
  });
});

describe('TurnStatusBar — token formatting', () => {
  it('displays percentage text', () => {
    render(<TurnStatusBar status={makeStatus({ context_tokens: 25000, context_max: 100000 })} />);
    expect(screen.getByText(/25%/)).toBeDefined();
  });

  it('formats large token counts with K suffix', () => {
    render(<TurnStatusBar status={makeStatus({ context_tokens: 25000, context_max: 100000 })} />);
    expect(screen.getByText(/25K\/100K/)).toBeDefined();
  });

  it('displays cost formatted to two decimal places', () => {
    render(<TurnStatusBar status={makeStatus({ turn_cost_usd: 0.0345 })} />);
    expect(screen.getByText('$0.03')).toBeDefined();
  });
});
