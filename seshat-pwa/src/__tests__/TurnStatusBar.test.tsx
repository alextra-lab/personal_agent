/**
 * Tests for TurnStatusBar component (ADR-0092 two-lane render).
 *
 * Session lane: cumulative cost, context occupancy, ⟳/↻ counts, ⚠ alert.
 * Engagement lane: tools X/Y (FRE-553).
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
    session_cost_usd: 0.45,
    session_context_tokens: 10000,
    compaction_count: 0,
    cache_reset_count: 0,
    quality_alert_count: 0,
    quality_alert: null,
    ...overrides,
  };
}

describe('TurnStatusBar — null renders nothing', () => {
  it('renders nothing when status is null', () => {
    const { container } = render(<TurnStatusBar status={null} />);
    expect(container.firstChild).toBeNull();
  });
});

describe('TurnStatusBar — session lane cost', () => {
  it('displays session_cost_usd formatted to two decimal places', () => {
    render(<TurnStatusBar status={makeStatus({ session_cost_usd: 1.2345 })} />);
    expect(screen.getByText('$1.23')).toBeDefined();
  });

  it('handles missing session_cost_usd gracefully (defaults to 0)', () => {
    const status = makeStatus();
    // Simulate old backend payload missing the field
    const partial = { ...status, session_cost_usd: undefined as unknown as number };
    render(<TurnStatusBar status={partial} />);
    expect(screen.getByText('$0.00')).toBeDefined();
  });
});

describe('TurnStatusBar — session lane context bar colour thresholds', () => {
  it('shows green bar below 70%', () => {
    render(
      <TurnStatusBar
        status={makeStatus({ session_context_tokens: 60000, context_max: 100000 })}
      />,
    );
    const bar = document.querySelector('.bg-emerald-500');
    expect(bar).not.toBeNull();
  });

  it('shows amber bar at exactly 70%', () => {
    render(
      <TurnStatusBar
        status={makeStatus({ session_context_tokens: 70000, context_max: 100000 })}
      />,
    );
    const bar = document.querySelector('.bg-amber-500');
    expect(bar).not.toBeNull();
  });

  it('shows amber bar between 70% and 84%', () => {
    render(
      <TurnStatusBar
        status={makeStatus({ session_context_tokens: 80000, context_max: 100000 })}
      />,
    );
    const bar = document.querySelector('.bg-amber-500');
    expect(bar).not.toBeNull();
  });

  it('shows red bar at exactly 85%', () => {
    render(
      <TurnStatusBar
        status={makeStatus({ session_context_tokens: 85000, context_max: 100000 })}
      />,
    );
    const bar = document.querySelector('.bg-red-500');
    expect(bar).not.toBeNull();
  });

  it('shows red bar above 85%', () => {
    render(
      <TurnStatusBar
        status={makeStatus({ session_context_tokens: 95000, context_max: 100000 })}
      />,
    );
    const bar = document.querySelector('.bg-red-500');
    expect(bar).not.toBeNull();
  });
});

describe('TurnStatusBar — session lane context percentage', () => {
  it('displays percentage text from session_context_tokens', () => {
    render(
      <TurnStatusBar
        status={makeStatus({ session_context_tokens: 25000, context_max: 100000 })}
      />,
    );
    expect(screen.getByText(/25%/)).toBeDefined();
  });

  it('formats large session token counts with K suffix', () => {
    render(
      <TurnStatusBar
        status={makeStatus({ session_context_tokens: 25000, context_max: 100000 })}
      />,
    );
    expect(screen.getByText(/25K\/100K/)).toBeDefined();
  });
});

describe('TurnStatusBar — compaction count ⟳', () => {
  it('hides compaction count when zero', () => {
    render(<TurnStatusBar status={makeStatus({ compaction_count: 0 })} />);
    expect(screen.queryByText(/⟳/)).toBeNull();
  });

  it('shows compaction count when greater than zero', () => {
    render(<TurnStatusBar status={makeStatus({ compaction_count: 3 })} />);
    expect(screen.getByText(/⟳.*3|3.*⟳/)).toBeDefined();
  });
});

describe('TurnStatusBar — cache reset count ↻', () => {
  it('hides cache reset count when zero', () => {
    render(<TurnStatusBar status={makeStatus({ cache_reset_count: 0 })} />);
    expect(screen.queryByText(/↻/)).toBeNull();
  });

  it('shows cache reset count when greater than zero', () => {
    render(<TurnStatusBar status={makeStatus({ cache_reset_count: 2 })} />);
    expect(screen.getByText(/↻.*2|2.*↻/)).toBeDefined();
  });
});

describe('TurnStatusBar — quality alert ⚠', () => {
  it('hides quality alert when null', () => {
    render(<TurnStatusBar status={makeStatus({ quality_alert: null })} />);
    expect(screen.queryByText(/⚠/)).toBeNull();
  });

  it('shows quality alert with red styling for high severity', () => {
    render(
      <TurnStatusBar
        status={makeStatus({
          quality_alert: { severity: 'high', phases_fired: ['memory_drop'] },
        })}
      />,
    );
    const alert = screen.getByText(/⚠/);
    expect(alert).toBeDefined();
    expect(alert.className).toContain('text-red-400');
  });

  it('shows quality alert with amber styling for low severity', () => {
    render(
      <TurnStatusBar
        status={makeStatus({
          quality_alert: { severity: 'low', phases_fired: ['history_trim'] },
        })}
      />,
    );
    const alert = screen.getByText(/⚠/);
    expect(alert).toBeDefined();
    expect(alert.className).toContain('text-amber-400');
  });

  it('falls back to amber for unknown severity', () => {
    render(
      <TurnStatusBar
        status={makeStatus({
          quality_alert: { severity: 'unknown_future_value', phases_fired: [] },
        })}
      />,
    );
    const alert = screen.getByText(/⚠/);
    expect(alert.className).toContain('text-amber-400');
  });
});

describe('TurnStatusBar — engagement lane tool iteration', () => {
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

  it('handles missing tool_iteration gracefully (defaults to 0)', () => {
    const partial = {
      ...makeStatus(),
      tool_iteration: undefined as unknown as number,
      tool_iteration_max: undefined as unknown as number,
    };
    render(<TurnStatusBar status={partial} />);
    expect(screen.getByText(/tools 0\/0/)).toBeDefined();
  });
});
