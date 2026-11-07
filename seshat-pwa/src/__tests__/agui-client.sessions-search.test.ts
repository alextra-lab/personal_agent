/**
 * Wire format of the conversation search (FRE-1746).
 *
 * `listSessions(limit, query)` calls `GET /api/v1/sessions?limit=N&q=<text>`. A missing or blank
 * query sends no `q`, so the call is the recent list as before.
 */

import { vi, describe, it, expect, beforeEach } from 'vitest';

import { listSessions } from '@/lib/agui-client';

function mockFetch() {
  const fn = vi
    .fn()
    .mockResolvedValue({ ok: true, status: 200, json: async () => [] });
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

function requestedUrl(fn: ReturnType<typeof mockFetch>): URL {
  return new URL(fn.mock.calls[0][0] as string, 'http://localhost');
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('listSessions — search query', () => {
  it('sends the query URL-encoded as q', async () => {
    const fn = mockFetch();
    await listSessions(50, 'a b&c=d#e');

    const url = requestedUrl(fn);
    expect(url.searchParams.get('limit')).toBe('50');
    expect(url.searchParams.get('q')).toBe('a b&c=d#e');
    expect(String(fn.mock.calls[0][0])).toContain('q=a%20b%26c%3Dd%23e');
  });

  it.each([undefined, '', '   '])('sends no q for %j', async (query) => {
    const fn = mockFetch();
    await listSessions(20, query);

    const url = requestedUrl(fn);
    expect(url.searchParams.has('q')).toBe(false);
    expect(url.searchParams.get('limit')).toBe('20');
  });

  it('keeps the one-argument call unchanged', async () => {
    const fn = mockFetch();
    await listSessions(1);

    expect(String(fn.mock.calls[0][0])).toMatch(/\/api\/v1\/sessions\?limit=1$/);
  });

  it('throws when the backend answers with an error', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 500 }) as unknown as typeof fetch;

    await expect(listSessions(50, 'x')).rejects.toThrow('listSessions failed: 500');
  });
});
