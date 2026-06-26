/**
 * Tests for SessionList component (FRE-521 — per-session turn count).
 *
 * Verifies:
 *   - turn_count is rendered as "N turns" / "1 turn" in the session list.
 *   - EVAL badge renders for channel=EVAL sessions (co-tested: FRE-522).
 *   - Loading and error states render correctly.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

vi.mock('@/lib/agui-client', () => ({
  listSessions: vi.fn(),
}));

// SessionList uses useRouter for navigation — mock next/navigation.
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

import { SessionList } from '@/components/SessionList';
import { listSessions } from '@/lib/agui-client';

const mockListSessions = listSessions as ReturnType<typeof vi.fn>;

const SESSION_BASE = {
  session_id: 'abc-123',
  created_at: new Date(Date.now() - 60_000).toISOString(),
  last_active_at: new Date(Date.now() - 60_000).toISOString(),
  mode: 'NORMAL',
  channel: null,
  execution_profile: 'local' as const,
  message_count: 10,
  title: 'Test session',
  turn_count: 5,
};

beforeEach(() => {
  mockListSessions.mockClear();
});

describe('SessionList — turn count display', () => {
  it('renders plural "turns" for turn_count > 1', async () => {
    mockListSessions.mockResolvedValue([{ ...SESSION_BASE, turn_count: 5 }]);
    render(<SessionList onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText(/5 turns/)).toBeInTheDocument());
  });

  it('renders singular "turn" for turn_count === 1', async () => {
    mockListSessions.mockResolvedValue([{ ...SESSION_BASE, turn_count: 1 }]);
    render(<SessionList onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText(/1 turn/)).toBeInTheDocument());
  });

  it('renders "0 turns" for an empty session', async () => {
    mockListSessions.mockResolvedValue([{ ...SESSION_BASE, turn_count: 0, title: null }]);
    render(<SessionList onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText(/0 turns/)).toBeInTheDocument());
  });
});

describe('SessionList — EVAL badge', () => {
  it('renders EVAL badge for channel=EVAL sessions', async () => {
    mockListSessions.mockResolvedValue([{ ...SESSION_BASE, channel: 'EVAL', turn_count: 3 }]);
    render(<SessionList onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText('EVAL')).toBeInTheDocument());
  });

  it('does not render EVAL badge for regular sessions', async () => {
    mockListSessions.mockResolvedValue([{ ...SESSION_BASE, channel: 'CHAT', turn_count: 3 }]);
    render(<SessionList onSelect={() => {}} />);
    await waitFor(() => expect(screen.queryByText('EVAL')).not.toBeInTheDocument());
  });
});

describe('SessionList — states', () => {
  it('shows loading state before fetch resolves', () => {
    mockListSessions.mockReturnValue(new Promise(() => {})); // never resolves
    render(<SessionList onSelect={() => {}} />);
    expect(screen.getByText('Loading…')).toBeInTheDocument();
  });

  it('shows error state when listSessions rejects', async () => {
    mockListSessions.mockRejectedValue(new Error('network error'));
    render(<SessionList onSelect={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText('Could not load sessions.')).toBeInTheDocument(),
    );
  });

  it('shows empty state when no sessions exist', async () => {
    mockListSessions.mockResolvedValue([]);
    render(<SessionList onSelect={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText('No prior conversations.')).toBeInTheDocument(),
    );
  });
});
