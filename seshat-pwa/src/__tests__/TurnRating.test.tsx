/**
 * Tests for the TurnRating control (FRE-757 redesign).
 *
 * Verifies the 3-chip control (error ✕ =0 / ok ✓ =2 / exceptional ★ =3):
 *   - Renders three tap-sized chips; resting default reads as "ok".
 *   - No network POST on mount (persist-on-send is the DONE hook's job).
 *   - Explicit click POSTs the chip's store value (manual, not default).
 *   - Hydration maps a stored 0/2/3 to the right chip; legacy 1 → legacy-low.
 *   - Re-rating overwrites; failure reverts.
 *   - Colors + 44px hit targets per the owner-confirmed spec.
 */

import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

// Mock submitTurnRating before importing the component (hoisted by Vitest).
vi.mock('@/lib/submitTurnRating', () => ({
  submitTurnRating: vi.fn().mockResolvedValue(true),
}));
vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
}));

import { TurnRating } from '@/components/TurnRating';
import { submitTurnRating } from '@/lib/submitTurnRating';

const mockSubmit = submitTurnRating as ReturnType<typeof vi.fn>;

const TRACE_ID = 'trace-abc-123';
const SESSION_ID = 'session-xyz-456';

const errorChip = () => screen.getByRole('button', { name: 'Rate error' });
const okChip = () => screen.getByRole('button', { name: 'Rate ok' });
const exceptionalChip = () => screen.getByRole('button', { name: 'Rate exceptional' });

beforeEach(() => {
  mockSubmit.mockClear();
  mockSubmit.mockResolvedValue(true);
});

describe('TurnRating — structure & resting default', () => {
  it('renders exactly three chips (error / ok / exceptional)', () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    expect(screen.getAllByRole('button')).toHaveLength(3);
    expect(errorChip()).toBeTruthy();
    expect(okChip()).toBeTruthy();
    expect(exceptionalChip()).toBeTruthy();
  });

  it('resting default selects "ok" (green), not error/exceptional', () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    expect(okChip().getAttribute('aria-pressed')).toBe('true');
    expect(okChip().className).toContain('bg-emerald-600');
    expect(errorChip().getAttribute('aria-pressed')).toBe('false');
    expect(exceptionalChip().getAttribute('aria-pressed')).toBe('false');
  });

  it('each chip is a 44×44px touch target', () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    for (const chip of screen.getAllByRole('button')) {
      expect(chip.className).toContain('h-11');
      expect(chip.className).toContain('w-11');
    }
  });

  it('applies the spec colors when a chip is selected', () => {
    // Separate renders: initialRating hydrates once at mount (a hydrated turn
    // never changes rating in place — it remounts), so props do not re-seed
    // state on rerender.
    const first = render(
      <TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} initialRating={0} />,
    );
    expect(errorChip().className).toContain('bg-red-800');
    first.unmount();

    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} initialRating={3} />);
    expect(exceptionalChip().className).toContain('bg-[#d4af37]');
  });

  it('does NOT call submitTurnRating on mount (persist-on-send is the DONE hook)', () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    expect(mockSubmit).not.toHaveBeenCalled();
  });
});

describe('TurnRating — explicit click', () => {
  it('posts 0 on error, 2 on ok, 3 on exceptional (manual, not default)', async () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);

    fireEvent.click(errorChip());
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    expect(mockSubmit).toHaveBeenLastCalledWith(TRACE_ID, SESSION_ID, 0);

    fireEvent.click(exceptionalChip());
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(2));
    expect(mockSubmit).toHaveBeenLastCalledWith(TRACE_ID, SESSION_ID, 3);

    fireEvent.click(okChip());
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(3));
    expect(mockSubmit).toHaveBeenLastCalledWith(TRACE_ID, SESSION_ID, 2);
    // The default flag is NEVER set by a manual click (4th positional arg absent/false).
    for (const call of mockSubmit.mock.calls) {
      expect(call[3] ?? false).toBe(false);
    }
  });

  it('re-rating overwrites (last click wins)', async () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    fireEvent.click(errorChip());
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    fireEvent.click(exceptionalChip());
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(2));
    expect(exceptionalChip().getAttribute('aria-pressed')).toBe('true');
    expect(errorChip().getAttribute('aria-pressed')).toBe('false');
  });
});

describe('TurnRating — hydration', () => {
  it('hydrates a stored 0 as error selected', () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} initialRating={0} />);
    expect(errorChip().getAttribute('aria-pressed')).toBe('true');
    expect(okChip().getAttribute('aria-pressed')).toBe('false');
  });

  it('hydrates a stored 3 as exceptional selected', () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} initialRating={3} />);
    expect(exceptionalChip().getAttribute('aria-pressed')).toBe('true');
  });

  it('hydrates a legacy stored 1 as a legacy-low state (no chip selected)', () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} initialRating={1} />);
    // No chip is in the selected state for the orphan legacy value.
    expect(okChip().getAttribute('aria-pressed')).toBe('false');
    expect(exceptionalChip().getAttribute('aria-pressed')).toBe('false');
    // The legacy-low affordance surfaces on the error side with its own label.
    const legacy = screen.getByRole('button', { name: /legacy low rating/i });
    expect(legacy).toBeTruthy();
  });
});

describe('TurnRating — failure revert', () => {
  it('reverts to the prior selection when the POST fails', async () => {
    mockSubmit.mockResolvedValue(false);
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} initialRating={2} />);

    fireEvent.click(exceptionalChip());
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));

    // After failure, selection settles back to the prior value (ok=2).
    await waitFor(() => expect(okChip().getAttribute('aria-pressed')).toBe('true'));
    expect(exceptionalChip().getAttribute('aria-pressed')).toBe('false');
  });
});
