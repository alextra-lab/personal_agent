/**
 * Tests for SessionList component (FRE-521 — per-session turn count).
 *
 * Verifies:
 *   - turn_count is rendered as "N turns" / "1 turn" in the session list.
 *   - EVAL badge renders for channel=EVAL sessions (co-tested: FRE-522).
 *   - Loading and error states render correctly.
 *   - session_label/session_digest rendering (ADR-0124 Phase 1, FRE-948).
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

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

describe('SessionList — label and digest (ADR-0124 Phase 1, FRE-948)', () => {
  it('renders session_label instead of the title when both are present', async () => {
    mockListSessions.mockResolvedValue([
      { ...SESSION_BASE, session_label: 'A generated label', title: 'Original message title' },
    ]);
    render(<SessionList onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText('A generated label')).toBeInTheDocument());
    expect(screen.queryByText('Original message title')).not.toBeInTheDocument();
  });

  it('falls back to title when session_label is null', async () => {
    mockListSessions.mockResolvedValue([{ ...SESSION_BASE, session_label: null }]);
    render(<SessionList onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText('Test session')).toBeInTheDocument());
  });

  it('renders digest text when present, including multiline content', async () => {
    mockListSessions.mockResolvedValue([
      { ...SESSION_BASE, session_digest: 'Established: \n- a fact\n\nDecisions: \n- a choice' },
    ]);
    render(<SessionList onSelect={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText(/Established:/)).toBeInTheDocument(),
    );
  });

  it('renders no digest element when session_digest is null', async () => {
    mockListSessions.mockResolvedValue([{ ...SESSION_BASE, session_digest: null }]);
    render(<SessionList onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText('Test session')).toBeInTheDocument());
    expect(screen.queryByText(/Established:/)).not.toBeInTheDocument();
  });

  it('does not crash when session_label/session_digest are undefined', async () => {
    // Simulates a cached service-worker response or a backend that hasn't
    // deployed yet — the two are independent deploys (ADR-0124 Phase 1 deploy note).
    mockListSessions.mockResolvedValue([SESSION_BASE]);
    render(<SessionList onSelect={() => {}} />);
    await waitFor(() => expect(screen.getByText('Test session')).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText(/5 turns/)).toBeInTheDocument());
  });
});

describe('SessionList — conversation search (FRE-1746)', () => {
  const RECENT = [{ ...SESSION_BASE, session_id: 'recent-1', title: 'Recent chat' }];
  const OLD_HIT = { ...SESSION_BASE, session_id: 'old-1', title: 'Old quokka chat' };
  const NEW_HIT = { ...SESSION_BASE, session_id: 'new-1', title: 'New quokka chat' };

  /** A promise the test settles by hand, to order responses. */
  function deferred<T>() {
    let resolve!: (v: T) => void;
    let reject!: (e: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  const flush = (ms = 0) =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });

  const type = (value: string) =>
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search conversations' }), {
      target: { value },
    });

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('has a search box and loads the recent list first', async () => {
    mockListSessions.mockResolvedValue(RECENT);
    render(<SessionList onSelect={() => {}} />);
    await flush();

    expect(screen.getByRole('searchbox', { name: 'Search conversations' })).toBeInTheDocument();
    expect(mockListSessions).toHaveBeenCalledTimes(1);
    expect(mockListSessions).toHaveBeenCalledWith(20);
    expect(screen.getByText('Recent chat')).toBeInTheDocument();
  });

  it('searches after a pause in typing and shows the matches (AC-1)', async () => {
    mockListSessions.mockImplementation(async (_limit: number, q?: string) =>
      q ? [NEW_HIT, OLD_HIT] : RECENT,
    );
    render(<SessionList onSelect={() => {}} />);
    await flush();

    type('quokka');
    await flush(299);
    expect(mockListSessions).not.toHaveBeenCalledWith(50, 'quokka');
    await flush(1);

    expect(mockListSessions).toHaveBeenCalledWith(50, 'quokka');
    expect(screen.getByText('New quokka chat')).toBeInTheDocument();
    expect(screen.getByText('Old quokka chat')).toBeInTheDocument();
    expect(screen.queryByText('Recent chat')).not.toBeInTheDocument();
  });

  it('sends one search for a burst of keystrokes', async () => {
    mockListSessions.mockResolvedValue(RECENT);
    render(<SessionList onSelect={() => {}} />);
    await flush();

    type('q');
    await flush(100);
    type('qu');
    await flush(100);
    type('quo');
    await flush(300);

    const searches = mockListSessions.mock.calls.filter(([limit]) => limit === 50);
    expect(searches).toEqual([[50, 'quo']]);
  });

  it('shows the recent list at once when the box is cleared (AC-3)', async () => {
    mockListSessions.mockImplementation(async (_limit: number, q?: string) =>
      q ? [OLD_HIT] : RECENT,
    );
    render(<SessionList onSelect={() => {}} />);
    await flush();
    type('quokka');
    await flush(300);
    expect(screen.getByText('Old quokka chat')).toBeInTheDocument();

    type('');
    await flush(0); // no timer advance: the empty path must not wait for the debounce

    expect(mockListSessions).toHaveBeenLastCalledWith(20);
    expect(screen.getByText('Recent chat')).toBeInTheDocument();
    expect(screen.queryByText('Old quokka chat')).not.toBeInTheDocument();
  });

  it('treats a blank box as empty and sends no search', async () => {
    mockListSessions.mockResolvedValue(RECENT);
    render(<SessionList onSelect={() => {}} />);
    await flush();

    type('   ');
    await flush(1000);

    expect(mockListSessions.mock.calls.filter(([limit]) => limit === 50)).toEqual([]);
    expect(screen.getByText('Recent chat')).toBeInTheDocument();
  });

  it('says when no conversation matches', async () => {
    mockListSessions.mockImplementation(async (_limit: number, q?: string) => (q ? [] : RECENT));
    render(<SessionList onSelect={() => {}} />);
    await flush();

    type('zzz');
    await flush(300);

    expect(screen.getByText('No conversations match “zzz”.')).toBeInTheDocument();
    expect(screen.queryByText('No prior conversations.')).not.toBeInTheDocument();
  });

  it('keeps the same search box, with focus, while results load', async () => {
    mockListSessions.mockImplementation((_limit: number, q?: string) =>
      q ? new Promise(() => {}) : Promise.resolve(RECENT),
    );
    render(<SessionList onSelect={() => {}} />);
    await flush();
    const box = screen.getByRole('searchbox', { name: 'Search conversations' });
    box.focus();

    type('quokka');
    await flush(300);

    expect(screen.getByText('Loading…')).toBeInTheDocument();
    expect(screen.getByRole('searchbox', { name: 'Search conversations' })).toBe(box);
    expect(document.activeElement).toBe(box);
  });

  it('keeps the search box when the search fails', async () => {
    mockListSessions.mockImplementation(async (_limit: number, q?: string) => {
      if (q) throw new Error('boom');
      return RECENT;
    });
    render(<SessionList onSelect={() => {}} />);
    await flush();

    type('quokka');
    await flush(300);

    expect(screen.getByText('Could not load sessions.')).toBeInTheDocument();
    expect(screen.getByRole('searchbox', { name: 'Search conversations' })).toBeInTheDocument();
  });

  it('ignores a late answer to an earlier search', async () => {
    const first = deferred<(typeof OLD_HIT)[]>();
    const second = deferred<(typeof NEW_HIT)[]>();
    mockListSessions.mockImplementation((_limit: number, q?: string) => {
      if (q === 'qu') return first.promise;
      if (q === 'quokka') return second.promise;
      return Promise.resolve(RECENT);
    });
    render(<SessionList onSelect={() => {}} />);
    await flush();

    type('qu');
    await flush(300);
    type('quokka');
    await flush(300);

    // The older request answers while the newer one is still pending.
    await act(async () => first.resolve([OLD_HIT]));
    expect(screen.queryByText('Old quokka chat')).not.toBeInTheDocument();
    expect(screen.getByText('Loading…')).toBeInTheDocument();

    // The newer request answers last and its results show.
    await act(async () => second.resolve([NEW_HIT]));
    expect(screen.getByText('New quokka chat')).toBeInTheDocument();
    expect(screen.queryByText('Loading…')).not.toBeInTheDocument();
  });

  it('ignores a late failure of an earlier search', async () => {
    const first = deferred<(typeof OLD_HIT)[]>();
    mockListSessions.mockImplementation((_limit: number, q?: string) => {
      if (q === 'qu') return first.promise;
      if (q === 'quokka') return Promise.resolve([NEW_HIT]);
      return Promise.resolve(RECENT);
    });
    render(<SessionList onSelect={() => {}} />);
    await flush();

    type('qu');
    await flush(300);
    type('quokka');
    await flush(300);
    expect(screen.getByText('New quokka chat')).toBeInTheDocument();

    await act(async () => first.reject(new Error('late failure')));

    expect(screen.queryByText('Could not load sessions.')).not.toBeInTheDocument();
    expect(screen.getByText('New quokka chat')).toBeInTheDocument();
  });

  it('does not let a late recent list overwrite a newer search', async () => {
    const recent = deferred<typeof RECENT>();
    mockListSessions.mockImplementation((_limit: number, q?: string) =>
      q ? Promise.resolve([NEW_HIT]) : recent.promise,
    );
    render(<SessionList onSelect={() => {}} />);
    await flush();

    type('quokka');
    await flush(300);
    expect(screen.getByText('New quokka chat')).toBeInTheDocument();

    await act(async () => recent.resolve(RECENT));

    expect(screen.queryByText('Recent chat')).not.toBeInTheDocument();
    expect(screen.getByText('New quokka chat')).toBeInTheDocument();
  });
});
