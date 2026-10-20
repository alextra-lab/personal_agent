/**
 * Tests for useSeshatEffort (ADR-0157 D2, FRE-1623 AC-1 and AC-2).
 *
 * The server holds the level. The hook hydrates it from the session GET and from the
 * `session_effort` STATE_DELTA, writes a change with one PATCH, and returns to the stored
 * level when the write fails.
 */

import { renderHook, waitFor, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

const mockGetSession = vi.fn();
const mockSetSessionEffort = vi.fn();

vi.mock('@/lib/agui-client', () => ({
  getSession: (...args: unknown[]) => mockGetSession(...args),
  setSessionEffort: (...args: unknown[]) => mockSetSessionEffort(...args),
}));

import { useSeshatEffort } from '@/hooks/useSeshatEffort';
import type { ServerEffort } from '@/hooks/useAgentStream';

function session(level: string | undefined) {
  return { session_id: 's1', seshat_effort: level };
}

function mount(sessionId: string | undefined = 's1', serverEffort: ServerEffort | null = null) {
  return renderHook(
    (props: { sid: string | undefined; delta: ServerEffort | null }) =>
      useSeshatEffort(props.sid, props.delta),
    { initialProps: { sid: sessionId, delta: serverEffort } },
  );
}

beforeEach(() => {
  mockGetSession.mockReset();
  mockSetSessionEffort.mockReset();
  localStorage.clear();
});

describe('AC-1 — hydration', () => {
  it('shows the level from the session GET, not a default or a cached one', async () => {
    localStorage.setItem('seshat_effort', 'fast'); // a stale cache must never win
    mockGetSession.mockResolvedValue(session('deep'));

    const { result } = mount();

    expect(result.current.level).toBeNull(); // unknown until the server answers
    await waitFor(() => expect(result.current.level).toBe('deep'));
    expect(mockGetSession).toHaveBeenCalledWith('s1');
  });

  it('shows the level of a STATE_DELTA that arrives after mount', async () => {
    mockGetSession.mockResolvedValue(session('deep'));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.level).toBe('deep'));

    rerender({ sid: 's1', delta: { sessionId: 's1', level: 'fast' } });

    await waitFor(() => expect(result.current.level).toBe('fast'));
  });

  it('shows the level of the reconnect STATE_DELTA, even when the GET answers later', async () => {
    let resolveGet: (v: unknown) => void = () => {};
    mockGetSession.mockReturnValue(new Promise((r) => (resolveGet = r)));
    const { result } = mount('s1', { sessionId: 's1', level: 'fast' });

    await waitFor(() => expect(result.current.level).toBe('fast'));
    await act(async () => resolveGet(session('standard'))); // an older read, answered late
    expect(result.current.level).toBe('fast');
  });

  it('ignores a STATE_DELTA that belongs to another session', async () => {
    mockGetSession.mockResolvedValue(session('standard'));
    const { result } = mount('s1', { sessionId: 'other', level: 'deep' });

    await waitFor(() => expect(result.current.level).toBe('standard'));
  });

  it('shows Standard for a new session (the GET finds no row)', async () => {
    mockGetSession.mockResolvedValue(null);

    const { result } = mount();

    await waitFor(() => expect(result.current.level).toBe('standard'));
    expect(result.current.sendLevel).toBe('standard');
  });

  it('shows no level when the GET fails, and sends Standard', async () => {
    mockGetSession.mockRejectedValue(new Error('network'));

    const { result } = mount();

    await act(async () => {});
    expect(result.current.level).toBeNull();
    expect(result.current.sendLevel).toBe('standard');
  });

  it('drops the previous session’s level when the session changes', async () => {
    mockGetSession.mockResolvedValueOnce(session('deep'));
    const { result, rerender } = mount('s1');
    await waitFor(() => expect(result.current.level).toBe('deep'));

    mockGetSession.mockResolvedValueOnce(session('fast'));
    rerender({ sid: 's2', delta: null });

    expect(result.current.level).toBeNull();
    await waitFor(() => expect(result.current.level).toBe('fast'));
  });
});

describe('ready — the composer holds Send until the GET answers', () => {
  it('is false while the GET is pending and true once it answers', async () => {
    let resolveGet: (v: unknown) => void = () => {};
    mockGetSession.mockReturnValue(new Promise((r) => (resolveGet = r)));
    const { result } = mount();

    expect(result.current.ready).toBe(false);
    await act(async () => resolveGet(session('deep')));

    expect(result.current.ready).toBe(true);
    expect(result.current.sendLevel).toBe('deep');
  });

  it('is true after a failed GET, so a network fault never blocks Send for good', async () => {
    mockGetSession.mockRejectedValue(new Error('network'));
    const { result } = mount();

    await waitFor(() => expect(result.current.ready).toBe(true));
  });
});

describe('AC-2 — the write and the revert', () => {
  it('sends one PATCH with the new level', async () => {
    mockGetSession.mockResolvedValue(session('standard'));
    mockSetSessionEffort.mockResolvedValue(undefined);
    const { result } = mount();
    await waitFor(() => expect(result.current.level).toBe('standard'));

    await act(async () => result.current.change('deep'));

    expect(mockSetSessionEffort).toHaveBeenCalledTimes(1);
    expect(mockSetSessionEffort).toHaveBeenCalledWith('s1', 'deep');
    expect(result.current.level).toBe('deep');
    expect(result.current.error).toBeNull();
  });

  it('shows the new level at once, before the write answers', async () => {
    mockGetSession.mockResolvedValue(session('standard'));
    mockSetSessionEffort.mockReturnValue(new Promise(() => {}));
    const { result } = mount();
    await waitFor(() => expect(result.current.level).toBe('standard'));

    act(() => result.current.change('fast'));

    expect(result.current.level).toBe('fast');
  });

  it('returns to the stored level and shows the error when the PATCH fails', async () => {
    mockGetSession.mockResolvedValue(session('standard'));
    mockSetSessionEffort.mockRejectedValue(new Error('setSessionEffort failed: 500'));
    const { result } = mount();
    await waitFor(() => expect(result.current.level).toBe('standard'));

    await act(async () => result.current.change('deep'));

    expect(mockSetSessionEffort).toHaveBeenCalledTimes(1);
    expect(result.current.level).toBe('standard');
    expect(result.current.sendLevel).toBe('standard');
    expect(result.current.error).toContain('500');
  });

  it('returns to the level of the last successful write when a later write fails', async () => {
    mockGetSession.mockResolvedValue(session('standard'));
    mockSetSessionEffort.mockResolvedValueOnce(undefined);
    const { result } = mount();
    await waitFor(() => expect(result.current.level).toBe('standard'));
    await act(async () => result.current.change('deep'));

    mockSetSessionEffort.mockRejectedValueOnce(new Error('setSessionEffort failed: 503'));
    await act(async () => result.current.change('fast'));

    expect(result.current.level).toBe('deep');
    expect(result.current.error).toContain('503');
  });

  it('clears the error on the next change', async () => {
    mockGetSession.mockResolvedValue(session('standard'));
    mockSetSessionEffort.mockRejectedValueOnce(new Error('setSessionEffort failed: 500'));
    const { result } = mount();
    await waitFor(() => expect(result.current.level).toBe('standard'));
    await act(async () => result.current.change('deep'));
    expect(result.current.error).not.toBeNull();

    mockSetSessionEffort.mockResolvedValueOnce(undefined);
    await act(async () => result.current.change('fast'));

    expect(result.current.error).toBeNull();
    expect(result.current.level).toBe('fast');
  });

  it('keeps the level for a session with no row yet (404); the first chat request sends it', async () => {
    mockGetSession.mockResolvedValue(null);
    mockSetSessionEffort.mockRejectedValue(new Error('setSessionEffort failed: 404'));
    const { result } = mount();
    await waitFor(() => expect(result.current.level).toBe('standard'));

    await act(async () => result.current.change('deep'));

    expect(result.current.level).toBe('deep');
    expect(result.current.sendLevel).toBe('deep');
    expect(result.current.error).toBeNull();
  });

  it('treats a 404 on a session that has a row as a failed write', async () => {
    mockGetSession.mockResolvedValue(session('standard'));
    mockSetSessionEffort.mockRejectedValue(new Error('setSessionEffort failed: 404'));
    const { result } = mount();
    await waitFor(() => expect(result.current.level).toBe('standard'));

    await act(async () => result.current.change('deep'));

    expect(result.current.level).toBe('standard');
    expect(result.current.error).toContain('404');
  });

  it('sends rapid changes one at a time, in order, so the last choice commits last', async () => {
    mockGetSession.mockResolvedValue(session('standard'));
    let finishFirst: () => void = () => {};
    mockSetSessionEffort
      .mockReturnValueOnce(new Promise<void>((resolve) => (finishFirst = resolve)))
      .mockResolvedValueOnce(undefined);
    const { result } = mount();
    await waitFor(() => expect(result.current.level).toBe('standard'));

    act(() => result.current.change('fast'));
    await act(async () => result.current.change('deep'));
    expect(mockSetSessionEffort).toHaveBeenCalledTimes(1); // the second waits for the first
    expect(mockSetSessionEffort).toHaveBeenLastCalledWith('s1', 'fast');

    await act(async () => finishFirst());

    await waitFor(() => expect(mockSetSessionEffort).toHaveBeenCalledTimes(2));
    expect(mockSetSessionEffort).toHaveBeenLastCalledWith('s1', 'deep');
    expect(result.current.level).toBe('deep');
  });

  it('does not let a failure of an older write undo a newer choice', async () => {
    mockGetSession.mockResolvedValue(session('standard'));
    let failFirst: (e: Error) => void = () => {};
    mockSetSessionEffort
      .mockReturnValueOnce(new Promise((_, reject) => (failFirst = reject)))
      .mockResolvedValueOnce(undefined);
    const { result } = mount();
    await waitFor(() => expect(result.current.level).toBe('standard'));

    act(() => result.current.change('fast'));
    await act(async () => result.current.change('deep'));
    await act(async () => failFirst(new Error('setSessionEffort failed: 500')));

    await waitFor(() => expect(mockSetSessionEffort).toHaveBeenCalledTimes(2));
    expect(result.current.level).toBe('deep');
    expect(result.current.error).toBeNull();
  });

  it('returns to the level the first write committed when the second write fails', async () => {
    mockGetSession.mockResolvedValue(session('standard'));
    mockSetSessionEffort
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error('setSessionEffort failed: 500'));
    const { result } = mount();
    await waitFor(() => expect(result.current.level).toBe('standard'));

    act(() => result.current.change('fast'));
    await act(async () => result.current.change('deep'));

    await waitFor(() => expect(result.current.error).toContain('500'));
    expect(result.current.level).toBe('fast');
  });

  it('keeps a server notice that arrives during a write when that write then succeeds', async () => {
    mockGetSession.mockResolvedValue(session('standard'));
    let finish: () => void = () => {};
    mockSetSessionEffort.mockReturnValueOnce(new Promise<void>((resolve) => (finish = resolve)));
    const { result, rerender } = mount();
    await waitFor(() => expect(result.current.level).toBe('standard'));

    act(() => result.current.change('fast'));
    rerender({ sid: 's1', delta: { sessionId: 's1', level: 'deep' } });
    await act(async () => finish());

    expect(result.current.level).toBe('deep');
    // The notice is the newer fact: a later failure returns to it, not to the older write.
    mockSetSessionEffort.mockRejectedValueOnce(new Error('setSessionEffort failed: 500'));
    await act(async () => result.current.change('fast'));
    expect(result.current.level).toBe('deep');
  });

  it('does not let the session GET overwrite a change made while it was loading', async () => {
    let resolveGet: (v: unknown) => void = () => {};
    mockGetSession.mockReturnValue(new Promise((r) => (resolveGet = r)));
    mockSetSessionEffort.mockResolvedValue(undefined);
    const { result } = mount();

    await act(async () => result.current.change('fast'));
    await act(async () => resolveGet(session('standard')));

    expect(result.current.level).toBe('fast');
  });
});
