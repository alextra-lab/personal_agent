/**
 * Tests for TurnRating component (FRE-407 refinement).
 *
 * Verifies:
 *   - Default visual display is segment 2 ("Meets expectation", sky-400) before any click.
 *   - No network POST on mount/render (submit helper not called on render).
 *   - Explicit click triggers the submit helper exactly once with the clicked rating.
 *   - Re-clicking the default segment (2) also triggers submit (deliberate confirm).
 */

import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

// Mock submitTurnRating before importing the component.
// The vi.mock call is hoisted to the top of the module by Vitest.
vi.mock('@/lib/submitTurnRating', () => ({
  submitTurnRating: vi.fn().mockResolvedValue(true),
}));

// Also mock agui-client (imported transitively via submitTurnRating real module
// path resolution in some bundler configs; belt-and-suspenders).
vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
}));

import { TurnRating } from '@/components/TurnRating';
import { submitTurnRating } from '@/lib/submitTurnRating';

const mockSubmit = submitTurnRating as ReturnType<typeof vi.fn>;

const TRACE_ID = 'trace-abc-123';
const SESSION_ID = 'session-xyz-456';

beforeEach(() => {
  mockSubmit.mockClear();
  mockSubmit.mockResolvedValue(true);
});

describe('TurnRating — default visual state', () => {
  it('renders 4 rating buttons', () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    // Each segment is a button with an aria-label matching its label
    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(4);
  });

  it('shows segment 2 (index 2, "Meets expectation") filled by default', () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    // Segments 0–2 should be filled (index <= currentRating=2), segment 3 unfilled.
    // We check via aria-label which maps to the RATING_META labels.
    const seg0 = screen.getByRole('button', { name: 'No value' });
    const seg1 = screen.getByRole('button', { name: 'Low value' });
    const seg2 = screen.getByRole('button', { name: 'Meets expectation' });
    const seg3 = screen.getByRole('button', { name: 'Wow' });

    // Segments 0–2 use the fill colour of the "Meets expectation" rating (sky-400).
    expect(seg0.className).toContain('bg-sky-400');
    expect(seg1.className).toContain('bg-sky-400');
    expect(seg2.className).toContain('bg-sky-400');
    // Segment 3 (above currentRating=2) is unfilled.
    expect(seg3.className).toContain('bg-slate-700');
  });

  it('does NOT call submitTurnRating on mount', () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    expect(mockSubmit).not.toHaveBeenCalled();
  });
});

describe('TurnRating — explicit click behaviour', () => {
  it('calls submitTurnRating with rating=0 when segment 0 is clicked', async () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    fireEvent.click(screen.getByRole('button', { name: 'No value' }));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    expect(mockSubmit).toHaveBeenCalledWith(TRACE_ID, SESSION_ID, 0);
  });

  it('calls submitTurnRating with rating=3 when segment 3 ("Wow") is clicked', async () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    fireEvent.click(screen.getByRole('button', { name: 'Wow' }));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    expect(mockSubmit).toHaveBeenCalledWith(TRACE_ID, SESSION_ID, 3);
  });

  it('calls submitTurnRating when segment 2 is explicitly clicked (deliberate confirm)', async () => {
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    // Clicking the default-filled segment still sends a POST.
    fireEvent.click(screen.getByRole('button', { name: 'Meets expectation' }));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    expect(mockSubmit).toHaveBeenCalledWith(TRACE_ID, SESSION_ID, 2);
  });

  it('only calls submit once per click (no duplicate on re-render)', async () => {
    const { rerender } = render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    fireEvent.click(screen.getByRole('button', { name: 'Low value' }));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    // Re-render without a click — still only one call.
    rerender(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);
    expect(mockSubmit).toHaveBeenCalledTimes(1);
  });
});

describe('TurnRating — failure revert', () => {
  it('reverts to visual default (2) on network failure after first click', async () => {
    mockSubmit.mockResolvedValue(false);
    render(<TurnRating traceId={TRACE_ID} sessionId={SESSION_ID} />);

    fireEvent.click(screen.getByRole('button', { name: 'Wow' }));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));

    // After failure, persisted stays null → visual default 2 restored.
    // Segment 2 should be filled with sky-400 again.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Meets expectation' }).className).toContain(
        'bg-sky-400',
      );
    });
    // Segment 3 should be back to slate-700 (not filled).
    expect(screen.getByRole('button', { name: 'Wow' }).className).toContain('bg-slate-700');
  });
});
