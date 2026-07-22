/**
 * Tests for the shared session-resolution helper (FRE-938).
 *
 * `resolveLastSessionId()` is the single fallback used by both the root
 * route and the Observe page when the localStorage key is missing: read
 * localStorage first (no network call), else ask the server for the most
 * recent session.
 */

import { vi, describe, it, expect, beforeEach } from 'vitest';

const mockListSessions = vi.fn();

vi.mock('@/lib/agui-client', () => ({
  listSessions: (...args: unknown[]) => mockListSessions(...args),
}));

import { LAST_SESSION_KEY, isValidUUID, resolveLastSessionId } from '@/lib/session';

beforeEach(() => {
  mockListSessions.mockReset();
  localStorage.clear();
});

describe('isValidUUID', () => {
  it('accepts a well-formed UUID', () => {
    expect(isValidUUID('123e4567-e89b-12d3-a456-426614174000')).toBe(true);
  });

  it('rejects garbage', () => {
    expect(isValidUUID('not-a-uuid')).toBe(false);
  });
});

describe('resolveLastSessionId', () => {
  it('returns the stored value without calling the server when present', async () => {
    localStorage.setItem(LAST_SESSION_KEY, '123e4567-e89b-12d3-a456-426614174000');

    const result = await resolveLastSessionId();

    expect(result).toBe('123e4567-e89b-12d3-a456-426614174000');
    expect(mockListSessions).not.toHaveBeenCalled();
  });

  it('asks the server for the most recent session when the key is absent', async () => {
    mockListSessions.mockResolvedValue([{ session_id: 'server-session-1' }]);

    const result = await resolveLastSessionId();

    expect(result).toBe('server-session-1');
    expect(mockListSessions).toHaveBeenCalledWith(1);
  });

  it('ignores a malformed stored value and asks the server instead', async () => {
    localStorage.setItem(LAST_SESSION_KEY, 'not-a-uuid');
    mockListSessions.mockResolvedValue([{ session_id: 'server-session-1' }]);

    const result = await resolveLastSessionId();

    expect(result).toBe('server-session-1');
  });

  it('returns undefined when the user has no sessions at all', async () => {
    mockListSessions.mockResolvedValue([]);

    const result = await resolveLastSessionId();

    expect(result).toBeUndefined();
  });

  it('returns undefined when the server lookup fails, rather than throwing', async () => {
    mockListSessions.mockRejectedValue(new Error('network down'));

    const result = await resolveLastSessionId();

    expect(result).toBeUndefined();
  });
});
